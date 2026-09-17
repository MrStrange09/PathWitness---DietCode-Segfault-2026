// Derive known bits from a shift-into-mask guard and hand them to LLVM.

#include "llvm/ADT/APInt.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/PassManager.h"
#include "llvm/IR/PatternMatch.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Plugins/PassPlugin.h"
#include "llvm/Support/raw_ostream.h"
#include <optional>

using namespace llvm;
using namespace llvm::PatternMatch;

namespace {

struct ShiftMaskGuard {
  Value *Source;      // the narrow value X whose bits we learn about
  APInt SourceMask;   // C, from `X & C`
  APInt SetMask;      // M, the set the shift result is tested against
  unsigned ShiftWidth;
  ICmpInst *Cmp;
  BranchInst *Branch;
};

// Match `((1 << (zext (X & C))) & M) ==/!= 0` feeding a conditional branch.
static std::optional<ShiftMaskGuard> matchShiftMask(ICmpInst *Cmp) {
  if (!Cmp->isEquality() || !Cmp->hasOneUse())
    return std::nullopt;
  auto *Br = dyn_cast<BranchInst>(*Cmp->user_begin());
  if (!Br || !Br->isConditional() || Br->getCondition() != Cmp)
    return std::nullopt;

  // The comparison must be against zero.
  Value *Masked = nullptr;
  if (match(Cmp->getOperand(1), m_Zero()))
    Masked = Cmp->getOperand(0);
  else if (match(Cmp->getOperand(0), m_Zero()))
    Masked = Cmp->getOperand(1);
  if (!Masked)
    return std::nullopt;

  const APInt *SetMask = nullptr;
  Value *Shifted = nullptr;
  if (!match(Masked, m_And(m_Value(Shifted), m_APInt(SetMask))))
    return std::nullopt;

  Value *Amount = nullptr;
  if (!match(Shifted, m_Shl(m_One(), m_Value(Amount))))
    return std::nullopt;

  // The shift amount may be widened before use; look through that.
  Value *Narrow = Amount;
  if (auto *Ext = dyn_cast<ZExtInst>(Amount))
    Narrow = Ext->getOperand(0);

  Value *Source = nullptr;
  const APInt *SourceMask = nullptr;
  if (!match(Narrow, m_And(m_Value(Source), m_APInt(SourceMask))))
    return std::nullopt;

  auto *ShiftType = dyn_cast<IntegerType>(Shifted->getType());
  auto *SourceType = dyn_cast<IntegerType>(Source->getType());
  if (!ShiftType || !SourceType || SourceType->getBitWidth() > 64)
    return std::nullopt;

  return ShiftMaskGuard{Source, *SourceMask, *SetMask,
                        ShiftType->getBitWidth(), Cmp, Br};
}

// Bits of X that are fixed on the edge where the masked shift is (non)zero.
// Enumerates every value `X & C` can take, which is exact, and keeps only the
// values consistent with the branch actually taken.
struct FixedBits {
  APInt Zero, One;
  bool Any() const { return Zero.getBoolValue() || One.getBoolValue(); }
};

static std::optional<FixedBits> deriveKnownBits(const ShiftMaskGuard &Guard,
                                                bool MaskedIsZero) {
  unsigned Width = Guard.SourceMask.getBitWidth();
  // `X & C` ranges exactly over the submasks of C. Bound the enumeration.
  if (Guard.SourceMask.popcount() > 12)
    return std::nullopt;

  const uint64_t SourceMask = Guard.SourceMask.getZExtValue();
  uint64_t Union = 0, Intersection = ~uint64_t(0);
  bool AnyCandidate = false;

  // Standard downward submask walk: C, then (sub - 1) & C, ending at 0.
  uint64_t Candidate = SourceMask;
  while (true) {
    // A shift at or beyond the shift type's width is poison, so such a value
    // cannot occur on a well-defined path. Excluding it is sound.
    if (Candidate < Guard.ShiftWidth) {
      bool InSet = Guard.SetMask[Candidate];
      // MaskedIsZero means the shifted bit missed the set.
      if (InSet != MaskedIsZero) {
        Union |= Candidate;
        Intersection &= Candidate;
        AnyCandidate = true;
      }
    }
    if (Candidate == 0)
      break;
    Candidate = (Candidate - 1) & SourceMask;
  }

  if (!AnyCandidate)
    return std::nullopt;   // edge is unreachable; do not assume false

  FixedBits Known{APInt(Width, ~Union & SourceMask),
                  APInt(Width, Intersection & SourceMask)};
  if (!Known.Any())
    return std::nullopt;
  return Known;
}

class PathWitnessShiftMask : public PassInfoMixin<PathWitnessShiftMask> {
public:
  // The assumption is placed at the top of a block with a single predecessor,
  // so no dominator information is needed.
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &) {
    SmallVector<ShiftMaskGuard, 4> Guards;
    for (BasicBlock &BB : F)
      for (Instruction &I : BB)
        if (auto *Cmp = dyn_cast<ICmpInst>(&I))
          if (auto Guard = matchShiftMask(Cmp))
            Guards.push_back(*Guard);
    if (Guards.empty())
      return PreservedAnalyses::all();

    unsigned Emitted = 0;
    constexpr unsigned MaxAssumes = 32;
    for (const ShiftMaskGuard &Guard : Guards) {
      if (Emitted >= MaxAssumes)
        break;
      for (unsigned Side = 0; Side != 2; ++Side) {
        BasicBlock *Target = Guard.Branch->getSuccessor(Side);
        // Only a sole entry edge carries the fact into the whole block.
        if (Target->getUniquePredecessor() != Guard.Branch->getParent())
          continue;
        // Successor 0 is taken when the condition is true.
        bool CondTrue = (Side == 0);
        bool MaskedIsZero = Guard.Cmp->getPredicate() == ICmpInst::ICMP_EQ
                                ? CondTrue
                                : !CondTrue;
        auto Known = deriveKnownBits(Guard, MaskedIsZero);
        if (!Known)
          continue;

        IRBuilder<> Builder(&*Target->getFirstInsertionPt());
        Type *Ty = Guard.Source->getType();
        if (Known->Zero.getBoolValue()) {
          Value *Bits = Builder.CreateAnd(
              Guard.Source, ConstantInt::get(Ty, Known->Zero), "pw.sm.zero");
          Builder.CreateAssumption(
              Builder.CreateICmpEQ(Bits, ConstantInt::get(Ty, 0)));
        }
        if (Known->One.getBoolValue()) {
          Value *Bits = Builder.CreateAnd(
              Guard.Source, ConstantInt::get(Ty, Known->One), "pw.sm.one");
          Builder.CreateAssumption(Builder.CreateICmpEQ(
              Bits, ConstantInt::get(Ty, Known->One)));
        }
        ++Emitted;
        errs() << "PW_SHIFT_MASK_ASSUME function=" << F.getName() << " source=";
        Guard.Source->printAsOperand(errs(), false);
        errs() << " source_mask=0x" << toString(Guard.SourceMask, 16, false)
               << " set_mask=0x" << toString(Guard.SetMask, 16, false)
               << " masked_is_zero=" << (MaskedIsZero ? "true" : "false")
               << " known_zero=0x" << toString(Known->Zero, 16, false)
               << " known_one=0x" << toString(Known->One, 16, false)
               << " block=";
        Target->printAsOperand(errs(), false);
        errs() << "\n";
      }
    }
    return Emitted ? PreservedAnalyses::none() : PreservedAnalyses::all();
  }
};

} // namespace

extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "PathWitnessShiftMask", LLVM_VERSION_STRING,
          [](PassBuilder &PB) {
            PB.registerPipelineParsingCallback(
                [](StringRef Name, FunctionPassManager &FPM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                  if (Name != "path-witness-shift-mask")
                    return false;
                  FPM.addPass(PathWitnessShiftMask());
                  return true;
                });
          }};
}
