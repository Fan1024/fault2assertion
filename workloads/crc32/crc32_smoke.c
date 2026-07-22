#include <stdint.h>

/*
 * Extremely small deterministic workload for CV32E40P
 * gate-level functional simulation.
 *
 * It intentionally avoids:
 *   - input files
 *   - CRC tables
 *   - dynamic memory
 *   - printf
 *   - libc calls
 *   - large arrays
 *
 * It still exercises:
 *   - integer ADD
 *   - XOR
 *   - variable shift
 *   - loop and branch
 *   - memory load/store through volatile objects
 */

/*
 * Volatile prevents the compiler from replacing the whole workload
 * with one precomputed constant.
 */
static volatile uint32_t smoke_a;
static volatile uint32_t smoke_b;
static volatile uint32_t smoke_c;
static volatile uint32_t smoke_i;

/*
 * Observable final signature retained in memory.
 *
 * Expected values:
 *   signature[0] = marker
 *   signature[1] = final a = 0x00000065
 *   signature[2] = final b = 0x00000007
 *   signature[3] = final c = 0x00006170
 */
volatile uint32_t f2a_smoke_signature[4];

int main(void)
{
    smoke_a = 5u;
    smoke_b = 7u;
    smoke_c = 0u;

    for (smoke_i = 0u; smoke_i < 32u; smoke_i++) {
        uint32_t shift_amount;

        shift_amount = smoke_i & 7u;

        smoke_c = smoke_c + smoke_a;
        smoke_c = smoke_c ^ (smoke_b << shift_amount);

        smoke_a = smoke_a + 3u;
        smoke_b = smoke_b ^ (smoke_a + smoke_i);
    }

    f2a_smoke_signature[0] = 0xF2A10001u;
    f2a_smoke_signature[1] = smoke_a;
    f2a_smoke_signature[2] = smoke_b;
    f2a_smoke_signature[3] = smoke_c;

    /*
     * Returning zero must use the same existing startup/exit mechanism
     * that previously produced "EXIT SUCCESS" for CRC32.
     */
    if ((smoke_a == 0x00000065u) &&
        (smoke_b == 0x00000007u) &&
        (smoke_c == 0x00006170u)) {
        return 0;
    }

    return 1;
}
