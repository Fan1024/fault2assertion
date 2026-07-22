///////////////////////////////////////////////////////////////////////////////
//
// Copyright 2020 OpenHW Group
//
// Licensed under the Solderpad Hardware Licence, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://solderpad.org/licenses/
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
///////////////////////////////////////////////////////////////////////////////
//
// Manifest for the CV32E40P RTL model.
//   - Intended to be used by both synthesis and simulation.
//   - Relevent synthesis and simulation scripts/Makefiles must set the shell
//     ENV variable DESIGN_RTL_DIR as required.
//
///////////////////////////////////////////////////////////////////////////////

+incdir+/raid/spring2026/fwu44/research/cv32e40p/rtl/include
+incdir+/raid/spring2026/fwu44/research/cv32e40p/rtl/../bhv
+incdir+/raid/spring2026/fwu44/research/cv32e40p/rtl/../bhv/include
+incdir+/raid/spring2026/fwu44/research/cv32e40p/rtl/../sva

/raid/spring2026/fwu44/research/cv32e40p/rtl/include/cv32e40p_apu_core_pkg.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/include/cv32e40p_fpu_pkg.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/include/cv32e40p_pkg.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_if_stage.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_cs_registers.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_register_file_ff.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_load_store_unit.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_id_stage.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_aligner.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_decoder.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_compressed_decoder.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_fifo.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_prefetch_buffer.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_hwloop_regs.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_mult.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_int_controller.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_ex_stage.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_alu_div.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_alu.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_ff_one.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_popcnt.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_apu_disp.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_controller.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_obi_interface.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_prefetch_controller.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_sleep_unit.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_core.sv

/raid/spring2026/fwu44/research/cv32e40p/rtl/cv32e40p_top.sv

/raid/spring2026/fwu44/research/cv32e40p/rtl/../bhv/cv32e40p_sim_clock_gate.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/../bhv/include/cv32e40p_tracer_pkg.sv
/raid/spring2026/fwu44/research/cv32e40p/rtl/../bhv/cv32e40p_tb_wrapper.sv
