cmake_minimum_required(VERSION 3.20)
project(PathWitness LANGUAGES CXX)
if(NOT CMAKE_CONFIGURATION_TYPES AND NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type for the experimental passes" FORCE)
endif()
find_package(LLVM REQUIRED CONFIG)
list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")
include(AddLLVM)
include_directories(SYSTEM ${LLVM_INCLUDE_DIRS})
add_definitions(${LLVM_DEFINITIONS})

add_llvm_pass_plugin(PathWitnessMaskWidth PathWitnessMaskWidth.cpp
                     PARTIAL_SOURCES_INTENDED)
target_compile_features(PathWitnessMaskWidth PRIVATE cxx_std_17)

# Kept as a separate plugin so work on the shift-mask derivation cannot disturb
# the mask-width pass, which the Alive2 submissions cover.
add_llvm_pass_plugin(PathWitnessShiftMask PathWitnessShiftMask.cpp
                     PARTIAL_SOURCES_INTENDED)
target_compile_features(PathWitnessShiftMask PRIVATE cxx_std_17)
