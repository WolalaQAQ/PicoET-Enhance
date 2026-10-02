// picoet-target -- disposable benign control target for P0 injection testing.
//
// Links libdl.so and libc.so, then sleeps forever. It exists only so the P0
// injector can be exercised end-to-end against a process that is safe to lose,
// before touching pxreyetrackingservice. No PICO bytes.

#define _GNU_SOURCE
#include <dlfcn.h>
#include <unistd.h>

int main(void)
{
    // Reference a libdl symbol so DT_NEEDED libdl.so is kept (--as-needed).
    (void)dlopen(NULL, RTLD_NOW);
    for (;;)
        pause();
    return 0;
}
