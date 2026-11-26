/* Long-running target for the attach and core-file checks. It allows any
   process to ptrace it, so the test does not depend on kernel.yama.ptrace_scope
   (Ubuntu and Mint default it to 1, which only permits ancestors). */
#include <stdio.h>
#include <unistd.h>

#ifdef __linux__
#include <sys/prctl.h>
#endif

static void busy_wait(int seconds) {
    for (int i = 0; i < seconds; i++) {
        sleep(1);
    }
}

int main(void) {
#ifdef __linux__
    prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0, 0, 0);
#endif
    printf("demo_loop pid=%d\n", (int)getpid());
    fflush(stdout);
    busy_wait(120);
    return 0;
}
