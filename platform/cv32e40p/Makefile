.RECIPEPREFIX := >

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/../..)

WORKLOAD     := crc32
WORKLOAD_DIR := $(ROOT)/workloads/$(WORKLOAD)
BUILD_DIR    := $(ROOT)/build/cv32e40p/$(WORKLOAD)

CV32E40P_HOME ?= $(abspath $(ROOT)/../cv32e40p)
TB_CORE         := $(CV32E40P_HOME)/example_tb/core
RUNTIME_DIR     := $(TB_CORE)/custom

ifeq ($(strip $(CROSS_COMPILE)),)
ifneq ($(strip $(RISCV)),)
CROSS_COMPILE := $(RISCV)/bin/riscv32-unknown-elf-
else
CROSS_COMPILE := riscv32-unknown-elf-
endif
endif

CC      := $(CROSS_COMPILE)gcc
OBJCOPY := $(CROSS_COMPILE)objcopy
OBJDUMP := $(CROSS_COMPILE)objdump
READELF := $(CROSS_COMPILE)readelf
NM      := $(CROSS_COMPILE)nm
SIZE    := $(CROSS_COMPILE)size

ARCH ?= rv32imc_zicsr_zifencei
ABI  ?= ilp32

ELF          := $(BUILD_DIR)/crc32.elf
HEX          := $(BUILD_DIR)/crc32.hex
BIN          := $(BUILD_DIR)/crc32.bin
MAP          := $(BUILD_DIR)/crc32.map
DIS          := $(BUILD_DIR)/crc32.dis
SYM          := $(BUILD_DIR)/crc32.sym
READELF_TXT  := $(BUILD_DIR)/crc32.readelf.txt
SIZE_TXT     := $(BUILD_DIR)/crc32.size.txt

APP_SRCS := \
    $(WORKLOAD_DIR)/crc_32.c \
    $(WORKLOAD_DIR)/crc32_baremetal.c

RUNTIME_SRCS := \
    $(RUNTIME_DIR)/crt0.S \
    $(RUNTIME_DIR)/vectors.S \
    $(RUNTIME_DIR)/syscalls.c

LINKER_SCRIPT := $(RUNTIME_DIR)/link.ld

CPPFLAGS := \
    -DCRC32_BAREMETAL \
    -I$(WORKLOAD_DIR)

CFLAGS := \
    -march=$(ARCH) \
    -mabi=$(ABI) \
    -O2 \
    -g3 \
    -std=gnu11 \
    -ffunction-sections \
    -fdata-sections \
    -fno-common \
    -Wall \
    -Wextra \
    -Wno-unused-parameter

LDFLAGS := \
    -march=$(ARCH) \
    -mabi=$(ABI) \
    -nostartfiles \
    -nostdlib \
    -static \
    -T$(LINKER_SCRIPT) \
    -Wl,--gc-sections \
    -Wl,--build-id=none \
    -Wl,-Map,$(MAP)

LDLIBS := \
    -Wl,--start-group \
    -lc \
    -lm \
    -lgcc \
    -Wl,--end-group

.PHONY: all check clean print-vars

all: $(ELF) $(HEX) $(BIN) $(DIS) $(SYM) $(READELF_TXT) $(SIZE_TXT)

check:
>command -v "$(CC)" >/dev/null || { \
>    echo "ERROR: RISC-V compiler not found: $(CC)"; \
>    echo "Set RISCV=/path/to/toolchain or CROSS_COMPILE=/path/to/riscv32-unknown-elf-"; \
>    exit 1; \
>}
>test -f "$(RUNTIME_DIR)/crt0.S" || { \
>    echo "ERROR: Missing $(RUNTIME_DIR)/crt0.S"; \
>    exit 1; \
>}
>test -f "$(RUNTIME_DIR)/vectors.S" || { \
>    echo "ERROR: Missing $(RUNTIME_DIR)/vectors.S"; \
>    exit 1; \
>}
>test -f "$(RUNTIME_DIR)/syscalls.c" || { \
>    echo "ERROR: Missing $(RUNTIME_DIR)/syscalls.c"; \
>    exit 1; \
>}
>test -f "$(LINKER_SCRIPT)" || { \
>    echo "ERROR: Missing $(LINKER_SCRIPT)"; \
>    exit 1; \
>}

$(BUILD_DIR):
>mkdir -p "$@"

$(ELF): $(APP_SRCS) $(RUNTIME_SRCS) $(LINKER_SCRIPT) | check $(BUILD_DIR)
>echo "Building RISC-V ELF: $@"
>$(CC) \
>    $(CPPFLAGS) \
>    $(CFLAGS) \
>    $(RUNTIME_SRCS) \
>    $(APP_SRCS) \
>    $(LDFLAGS) \
>    $(LDLIBS) \
>    -o "$@"

$(HEX): $(ELF)
>echo "Generating Verilog memory HEX: $@"
>$(OBJCOPY) -O verilog "$<" "$@"

$(BIN): $(ELF)
>echo "Generating raw binary: $@"
>$(OBJCOPY) -O binary "$<" "$@"

$(DIS): $(ELF)
>echo "Generating disassembly: $@"
>$(OBJDUMP) -d -S "$<" > "$@"

$(SYM): $(ELF)
>echo "Generating symbol table: $@"
>$(NM) -n "$<" > "$@"

$(READELF_TXT): $(ELF)
>echo "Generating ELF report: $@"
>$(READELF) -h -S -s "$<" > "$@"

$(SIZE_TXT): $(ELF)
>echo "Generating size report: $@"
>$(SIZE) "$<" | tee "$@"

print-vars:
>@echo "ROOT             = $(ROOT)"
>@echo "CV32E40P_HOME    = $(CV32E40P_HOME)"
>@echo "RUNTIME_DIR      = $(RUNTIME_DIR)"
>@echo "WORKLOAD_DIR     = $(WORKLOAD_DIR)"
>@echo "BUILD_DIR        = $(BUILD_DIR)"
>@echo "CROSS_COMPILE    = $(CROSS_COMPILE)"
>@echo "ARCH             = $(ARCH)"
>@echo "ABI              = $(ABI)"
>@echo "ELF              = $(ELF)"
>@echo "HEX              = $(HEX)"

clean:
>rm -rf "$(BUILD_DIR)"
