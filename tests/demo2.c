/* Second program for the multi-session checks: its own functions and variable
   names, so a value read in one session cannot come from the other. */
#include <stdio.h>

static int depth = 7;

static int worker(int scale) {
    int scaled = depth * scale;
    return scaled;
}

int main(void) {
    int result = worker(6);
    printf("depth=%d result=%d\n", depth, result);
    return 0;
}
