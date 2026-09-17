// Two profitability witnesses for the same local mask-width identity:
// (1) all users can narrow together, eliminating a shared zero extension;
// (2) a dominated conflicting query motivates narrowing one zero-mask guard.
// No application names, memory substitution, or new path-fact engine.
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/Dominators.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/PassManager.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Plugins/PassPlugin.h"
#include "llvm/Support/raw_ostream.h"
#include <optional>

using namespace llvm;

namespace {
struct MaskCheck {
  ICmpInst *Cmp;
  BinaryOperator *And;
  Value *Input;
  APInt Mask;
  APInt Expected;
  BranchInst *Branch;
};

// Scalar equality/non-equality of an AND with constants, feeding one branch.
// Both operand orders are accepted. Reject unsupported shapes conservatively.
static std::optional<MaskCheck> matchCheck(ICmpInst *Cmp, bool BranchOnly = true) {
  if (!Cmp->isEquality() || !Cmp->getType()->isIntegerTy(1))
    return std::nullopt;
  auto *Br = Cmp->hasOneUse() ? dyn_cast<BranchInst>(*Cmp->user_begin()) : nullptr;
  if (BranchOnly && (!Br || !Br->isConditional() || Br->getCondition() != Cmp))
    return std::nullopt;

  auto *Expected = dyn_cast<ConstantInt>(Cmp->getOperand(1));
  Value *Masked = Cmp->getOperand(0);
  if (!Expected) {
    Expected = dyn_cast<ConstantInt>(Cmp->getOperand(0));
    Masked = Cmp->getOperand(1);
  }
  auto *And = dyn_cast<BinaryOperator>(Masked);
  if (!Expected || !And || And->getOpcode() != Instruction::And ||
      !And->getType()->isIntegerTy() ||
      And->getType()->getIntegerBitWidth() > 64)
    return std::nullopt;
  auto *Mask = dyn_cast<ConstantInt>(And->getOperand(1));
  Value *Input = And->getOperand(0);
  if (!Mask) {
    Mask = dyn_cast<ConstantInt>(And->getOperand(0));
    Input = And->getOperand(1);
  }
  if (!Mask || !(Expected->getValue() & ~Mask->getValue()).isZero())
    return std::nullopt;
  return MaskCheck{Cmp, And, Input, Mask->getValue(), Expected->getValue(), Br};
}

static void narrowCheck(const MaskCheck &Check, Value *X) {
  unsigned W = X->getType()->getIntegerBitWidth();
  IRBuilder<> B(Check.Cmp);
  B.SetCurrentDebugLocation(Check.Cmp->getDebugLoc());
  Check.Cmp->setOperand(0, B.CreateAnd(
      X, ConstantInt::get(X->getType(), Check.Mask.trunc(W)), "pw.mask"));
  Check.Cmp->setOperand(1, ConstantInt::get(X->getType(), Check.Expected.trunc(W)));
  // A positive wide mask result can become negative in the narrow type.
  // Keeping samesign would introduce poison, so discard that optional promise.
  Check.Cmp->setSameSign(false);
  Check.And->eraseFromParent();
}

static unsigned narrowAllUsers(Function &F) {
  SmallVector<ZExtInst *, 16> Extensions;
  for (auto &BB : F)
    for (auto &I : BB)
      if (auto *Ext = dyn_cast<ZExtInst>(&I))
        Extensions.push_back(Ext);
  unsigned Changes = 0, Probes = 0;
  for (auto *Ext : Extensions) {
    if (Changes >= 64 || Probes >= 4096) break;
    Value *X = Ext->getOperand(0);
    if (!X->getType()->isIntegerTy() || !Ext->getType()->isIntegerTy() ||
        Ext->getType()->getIntegerBitWidth() > 64 || Ext->hasNonNeg() ||
        isa<UndefValue>(X) || Ext->use_empty() || Ext->hasOneUse()) continue;
    unsigned Width = X->getType()->getIntegerBitWidth();
    SmallVector<MaskCheck, 4> Checks;
    bool All = true;
    for (User *U : Ext->users()) {
      if (++Probes > 4096 || Checks.size() + Changes >= 64) { All = false; break; }
      auto *And = dyn_cast<BinaryOperator>(U);
      auto *Cmp = And && And->hasOneUse() ? dyn_cast<ICmpInst>(*And->user_begin()) : nullptr;
      auto Check = Cmp ? matchCheck(Cmp, false) : std::nullopt;
      if (!Check || Check->Input != Ext || !Check->Expected.isIntN(Width)) {
        All = false;
        break;
      }
      Checks.push_back(*Check);
    }
    if (!All) continue; // An unhandled wide consumer prevents this group rewrite.
    for (const auto &Check : Checks) {
      errs() << "PW_MASK_NORMALIZED function=" << F.getName() << " source=";
      X->printAsOperand(errs(), false);
      errs() << " reason=all-users from=i" << Check.Mask.getBitWidth()
             << " to=i" << Width << "\n";
      narrowCheck(Check, X);
      ++Changes;
    }
    // All mask results have one user: no computation was duplicated. This
    // extension now has no users, yielding at least one fewer IR instruction.
    assert(Ext->use_empty());
    Ext->eraseFromParent();
  }
  return Changes;
}

class PathWitnessMaskWidth : public PassInfoMixin<PathWitnessMaskWidth> {
public:
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &FAM) {
    unsigned GroupChanges = narrowAllUsers(F);
    DenseMap<Value *, SmallVector<MaskCheck, 2>> Queries;
    SmallVector<MaskCheck, 8> Guards;
    for (BasicBlock &BB : F)
      for (Instruction &I : BB)
        if (auto *Cmp = dyn_cast<ICmpInst>(&I))
          if (auto Check = matchCheck(Cmp)) {
            Queries[Check->Input].push_back(*Check);
            if (Check->Expected.isZero())
              Guards.push_back(*Check);
          }

    if (Guards.empty())
      return GroupChanges ? PreservedAnalyses::none() : PreservedAnalyses::all();
    DominatorTree &DT = FAM.getResult<DominatorTreeAnalysis>(F);
    // Collection is linear. Bound the additional candidate/dominance work and
    // rewrites per function. These are engineering limits, not time measurements.
    constexpr unsigned MaxProbes = 4096, MaxRewrites = 64;
    unsigned Probes = 0, Rewrites = 0;
    for (const MaskCheck &Guard : Guards) {
      if (Probes >= MaxProbes || Rewrites >= MaxRewrites)
        break;
      auto *Ext = dyn_cast<ZExtInst>(Guard.Input);
      if (!Ext || Ext->hasNonNeg() || !Guard.And->hasOneUse())
        continue;
      Value *X = Ext->getOperand(0);
      if (!X->getType()->isIntegerTy() || isa<UndefValue>(X))
        continue;
      auto It = Queries.find(X);
      if (It == Queries.end())
        continue;
      unsigned Width = X->getType()->getIntegerBitWidth();
      APInt NarrowMask = Guard.Mask.trunc(Width);
      if (NarrowMask.isZero())
        continue;
      unsigned ZeroEdge = Guard.Cmp->getPredicate() == ICmpInst::ICMP_EQ ? 0 : 1;
      BasicBlockEdge Edge(Guard.Branch->getParent(),
                          Guard.Branch->getSuccessor(ZeroEdge));
      // A conditional branch has exactly two successors. Equal destinations
      // mean this block pair does not identify one particular branch outcome.
      // Spell this directly: LLVM 23 removed BasicBlockEdge::isSingleEdge().
      if (Guard.Branch->getSuccessor(0) == Guard.Branch->getSuccessor(1))
        continue;

      for (const MaskCheck &Query : It->second) {
        if (Probes >= MaxProbes)
          break;
        ++Probes;
        APInt Conflict = NarrowMask & Query.Expected;
        if (Conflict.isZero() || !DT.dominates(Edge, Query.Cmp->getParent()))
          continue;

        errs() << "PW_MASK_NORMALIZED function=" << F.getName() << " source=";
        X->printAsOperand(errs(), false);
        errs() << " from=i" << Guard.Mask.getBitWidth() << " to=i" << Width
               << " zero_edge=" << (ZeroEdge == 0 ? "true" : "false")
               << " zero_mask=0x" << toString(NarrowMask, 16, false)
               << " query_value=0x" << toString(Query.Expected, 16, false)
               << " conflicting_bits=0x" << toString(Conflict, 16, false)
               << " query_block=";
        Query.Cmp->getParent()->printAsOperand(errs(), false);
        errs() << "\n";

        // Dominance/conflict above selects a useful place to normalize. The
        // rewrite itself is a local bit identity, independently of path facts.
        // No freeze is bypassed, no load is duplicated, and the zext's other
        // users keep using the original instruction. The one-use AND is replaced
        // by one narrow AND, so this does not duplicate a live mask computation.
        narrowCheck(Guard, X);
        ++Rewrites;
        break;
      }
    }
    // The CFG topology is unchanged, so DT was usable throughout this run.
    // Conservatively invalidate cached analyses after changing instructions.
    return Rewrites || GroupChanges ? PreservedAnalyses::none() : PreservedAnalyses::all();
  }
};
} // namespace

extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "PathWitnessMaskWidth", LLVM_VERSION_STRING,
          [](PassBuilder &PB) {
            PB.registerPipelineParsingCallback(
                [](StringRef Name, FunctionPassManager &FPM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                  if (Name != "path-witness-mask-width")
                    return false;
                  FPM.addPass(PathWitnessMaskWidth());
                  return true;
                });
          }};
}
