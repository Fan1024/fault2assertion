#include <stdint.h>
#include <stdio.h>

#include "crc.h"

#define CRC32_DATA_SIZE           4096u
#define CRC32_ROUNDS              16u

#define CRC32_VECTOR_EXPECTED     0xCBF43926u
#define CRC32_SIGNATURE_EXPECTED  0x2D6352B3u

#define CRC32_STATUS_RUNNING      0x43524300u
#define CRC32_STATUS_PASS         0x43524350u
#define CRC32_STATUS_FAIL_VECTOR  0x43524601u
#define CRC32_STATUS_FAIL_RESULT  0x43524602u

/*
 * These variables intentionally remain externally visible.
 *
 * Later, crc32.sym can be used to obtain their addresses so that the
 * testbench or assertions can observe workload progress and results.
 */
volatile uint32_t g_crc32_last      = 0u;
volatile uint32_t g_crc32_signature = 0u;
volatile uint32_t g_crc32_status    = 0u;

static uint8_t g_crc32_data[CRC32_DATA_SIZE];

static uint32_t rotl32(uint32_t value, uint32_t shift)
{
    return (value << shift) | (value >> (32u - shift));
}

/*
 * Deterministic xorshift32 input generation.
 *
 * A fixed seed guarantees that RTL, mapped-netlist and fault-netlist
 * simulations all execute exactly the same workload.
 */
static void initialize_input(void)
{
    uint32_t state = 0x12345678u;

    for (uint32_t i = 0u; i < CRC32_DATA_SIZE; ++i)
    {
        state ^= state << 13;
        state ^= state >> 17;
        state ^= state << 5;

        g_crc32_data[i] = (uint8_t)(state >> 24);
    }
}

static uint32_t run_crc32_workload(void)
{
    uint32_t signature = 0x13579BDFu;

    for (uint32_t round = 0u; round < CRC32_ROUNDS; ++round)
    {
        uint32_t crc = (uint32_t)crc32buf(
            (char *)g_crc32_data,
            (size_t)CRC32_DATA_SIZE
        );

        g_crc32_last = crc;

        /*
         * Mix every round into one final deterministic signature.
         */
        signature = rotl32(signature, 5u);
        signature ^= crc + 0x9E3779B9u + round;

        /*
         * Change one byte before the next round.
         * CRC32_DATA_SIZE is 4096, so the mask performs modulo 4096.
         */
        uint32_t index = (round * 257u) & (CRC32_DATA_SIZE - 1u);
        uint32_t shift = (round & 3u) * 8u;

        g_crc32_data[index] ^= (uint8_t)(crc >> shift);
    }

    return signature;
}

int main(void)
{
    char standard_vector[] = "123456789";

    g_crc32_status = CRC32_STATUS_RUNNING;

    /*
     * Standard IEEE CRC-32 check:
     * CRC32("123456789") must be 0xCBF43926.
     */
    uint32_t vector_crc = (uint32_t)crc32buf(
        standard_vector,
        sizeof(standard_vector) - 1u
    );

    if (vector_crc != CRC32_VECTOR_EXPECTED)
    {
        g_crc32_last = vector_crc;
        g_crc32_status = CRC32_STATUS_FAIL_VECTOR;

        printf(
            "CRC32 FAIL: vector=%08lx expected=%08lx\n",
            (unsigned long)vector_crc,
            (unsigned long)CRC32_VECTOR_EXPECTED
        );

        return 1;
    }

    initialize_input();

    uint32_t signature = run_crc32_workload();
    g_crc32_signature = signature;

    if (signature != CRC32_SIGNATURE_EXPECTED)
    {
        g_crc32_status = CRC32_STATUS_FAIL_RESULT;

        printf(
            "CRC32 FAIL: signature=%08lx expected=%08lx last=%08lx\n",
            (unsigned long)signature,
            (unsigned long)CRC32_SIGNATURE_EXPECTED,
            (unsigned long)g_crc32_last
        );

        return 2;
    }

    g_crc32_status = CRC32_STATUS_PASS;

    printf(
        "CRC32 PASS: vector=%08lx signature=%08lx last=%08lx\n",
        (unsigned long)vector_crc,
        (unsigned long)signature,
        (unsigned long)g_crc32_last
    );

    return 0;
}
