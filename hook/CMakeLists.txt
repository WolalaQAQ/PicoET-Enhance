cmake_minimum_required(VERSION 3.22)
project(picoet_hook C ASM)

# ---- ShadowHook 2.0.1 (vendored source, static-in-object form) --------------
# Vendored upstream source (see third_party/shadowhook-2.0.1/VENDOR.txt). The
# vendored source carries a single local patch guarded by
# PICOET_SHADOWHOOK_NO_LINKER_MONITOR.
#
# Built as an OBJECT library (not an archive) on purpose: ShadowHook's errno
# module is initialised in its own `__attribute__((constructor))` and our
# constructor calls shadowhook_init(). Listing the ShadowHook objects before
# ours makes sh_errno_ctor enter .init_array first, so the TLS errno key exists
# by the time we init. (Verified offline from .init_array order.)
set(SH_DIR ${CMAKE_CURRENT_SOURCE_DIR}/third_party/shadowhook-2.0.1/cpp)
file(GLOB SH_SRC
    ${SH_DIR}/*.c
    ${SH_DIR}/arch/arm64/*.c
    ${SH_DIR}/arch/arm64/*.S
    ${SH_DIR}/common/*.c
    ${SH_DIR}/third_party/*/*.c)
list(FILTER SH_SRC EXCLUDE REGEX ".*/sh_jni\\.c$")   # no Java API; defines JNI_OnLoad

add_library(shadowhook_obj OBJECT ${SH_SRC})
set_target_properties(shadowhook_obj PROPERTIES C_STANDARD 11 POSITION_INDEPENDENT_CODE ON)
target_compile_definitions(shadowhook_obj PRIVATE PICOET_SHADOWHOOK_NO_LINKER_MONITOR)
target_compile_options(shadowhook_obj PRIVATE -Os -fPIC -fvisibility=hidden -ffunction-sections -fdata-sections -w)
target_include_directories(shadowhook_obj PRIVATE
    ${SH_DIR} ${SH_DIR}/arch/arm64 ${SH_DIR}/common
    ${SH_DIR}/third_party/xdl ${SH_DIR}/third_party/bsd ${SH_DIR}/third_party/lss
    PUBLIC ${SH_DIR}/include)

# ---- our payload object (placed after ShadowHook for ctor ordering) ---------
# -fno-omit-frame-pointer: the P4 Hook A proxy reads the caller frame (fp[1]
# caller return address, [[fp[0]]+0x10] timestamp).
add_library(picoet_obj OBJECT src/picoet_hook.c src/picoet_leaf.S)
set_target_properties(picoet_obj PROPERTIES C_STANDARD 11 POSITION_INDEPENDENT_CODE ON)
target_compile_options(picoet_obj PRIVATE -O2 -fPIC -fvisibility=hidden -Wall -Wextra)
set_source_files_properties(src/picoet_hook.c PROPERTIES COMPILE_OPTIONS "-fno-omit-frame-pointer")
target_include_directories(picoet_obj PRIVATE ${SH_DIR}/include)

add_library(picoet_hook SHARED
    $<TARGET_OBJECTS:shadowhook_obj>
    $<TARGET_OBJECTS:picoet_obj>)
set_target_properties(picoet_hook PROPERTIES OUTPUT_NAME picoet_hook PREFIX "lib")
target_link_options(picoet_hook PRIVATE
    "-Wl,-soname,libpicoet_hook.so"
    "-Wl,--gc-sections"
    "-Wl,-z,max-page-size=16384")
target_link_libraries(picoet_hook PRIVATE log dl)

# ---- injector + benign control target --------------------------------------
add_executable(picoet-inject src/picoet-inject.c)
target_compile_options(picoet-inject PRIVATE -O2 -fPIE -Wall -Wextra)
target_link_libraries(picoet-inject PRIVATE dl)

add_executable(picoet-target src/picoet-target.c)
target_compile_options(picoet-target PRIVATE -O2 -fPIE -Wall -Wextra)
target_link_libraries(picoet-target PRIVATE dl)
