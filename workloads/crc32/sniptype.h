#ifndef SNIPTYPE__H
#define SNIPTYPE__H

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef enum
{
    Error_   = -1,
    Success_ = 0,
    False_   = 0,
    True_    = 1
} Boolean_T;

typedef uint8_t  BYTE;
typedef uint16_t WORD;
typedef uint32_t DWORD;

typedef int8_t   SBYTE;
typedef int16_t  SWORD;
typedef int32_t  SDWORD;

typedef uint8_t  UNS_8_BITS;
typedef uint16_t UNS_16_BITS;
typedef uint32_t UNS_32_BITS;

typedef int8_t   S_8_BITS;
typedef int16_t  S_16_BITS;
typedef int32_t  S_32_BITS;

#endif
