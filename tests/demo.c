/* Small program for the MCP smoke test: a struct, a loop and a call chain, so a
   breakpoint has something interesting to show. */
#include <stdio.h>

typedef struct {
    int hits;
    double weight;
} Counter;

static int add(int a, int b) {
    int sum = a + b;
    return sum;
}

static void accumulate(Counter *counter, int value) {
    for (int i = 0; i < value; i++) {
        counter->hits = add(counter->hits, 1);
    }
    counter->weight = counter->hits * 0.5;
}

int main(void) {
    Counter counter = {0, 0.0};
    accumulate(&counter, 3);
    printf("hits=%d weight=%.1f\n", counter.hits, counter.weight);
    return 0;
}
