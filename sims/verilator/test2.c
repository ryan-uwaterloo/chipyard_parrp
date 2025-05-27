#include <stdlib.h>
#include <stdint.h>
#include <riscv-pk/encoding.h>
#include <stdio.h>
#include "marchid.h"

#define BLOCK_SIZE      64          // 64-byte cache lines
#define NUM_SETS        128         // smaller for testing
#define ASSOCIATIVITY   4
#define NUM_ACCESSES    100
#define SET_STRIDE      (NUM_SETS * BLOCK_SIZE)
#define ARRAY_SIZE 128   // Size of the array
#define STRIDE 2  // Step size for striding access

// EDIT THIS
static size_t n_cores = 1;


static void __attribute__((noinline)) barrier()
{
  static volatile int sense;
  static volatile int count;
  static __thread int threadsense;

  __sync_synchronize();

  threadsense = !threadsense;
  if (__sync_fetch_and_add(&count, 1) == n_cores-1)
  {
    count = 0;
    sense = threadsense;
  }
  else while(sense != threadsense)
    ;

  __sync_synchronize();
}

int __main(void) {
  size_t mhartid = read_csr(mhartid);

  // Small buffer to simulate addresses (only a few KB)
  size_t buffer_size = BLOCK_SIZE * ASSOCIATIVITY;

  if (mhartid >= n_cores) while (1);

  char *buffer = malloc(buffer_size);

  if (!buffer) {
    perror("malloc failed");
    return 1;
}

  const char* march = get_march(read_csr(marchid));
  for (size_t i = 0; i < n_cores; i++) {
    if (mhartid == i) {
      printf("Hello world from core %lu, a %s\n", mhartid, march);

      // Stress the replacement policy by rotating reads
      for (int i = 0; i < NUM_ACCESSES; ++i) {
        if ((i % 3) == 0){
            buffer[(i * 64 + i)%buffer_size] += 1; //modify *some* accesses
        }
        printf("data[%d] = %d\n", i, buffer[(i * 64)%buffer_size + i]);
      }
    //   printf("data[%d] = %d\n", i, buffer[(i * 64)%buffer_size + i]);

      free(buffer);
    }
    //barrier();
  }

  // Spin if not core 0
  if (mhartid > 0) while (1);
  return(0);
}

int main(void) {
  __main();
  return 0;
}
