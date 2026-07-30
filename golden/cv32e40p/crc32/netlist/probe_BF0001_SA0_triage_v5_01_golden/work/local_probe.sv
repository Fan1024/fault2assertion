`timescale 1ns/1ps

// Auto-generated local probe for BF0001_SA0.
//
// This monitor is observation-only. It does not drive design signals.
//
// tb_top owns riscy_tb.vcd through +vcd.  This monitor waits one delta cycle
// (#0), still at simulation time zero, and then adds its f2a_* variables before
// the VCD header is finalized.
module f2a_local_probe (
    input wire probe_source_original_i,
    input wire probe_branch_observed_i,
    input wire probe_input_A_i,
    input wire probe_input_B1_i,
    input wire probe_input_B2_i,
    input wire probe_input_C2_i,
    input wire probe_output_ZN_i
);

  (* keep = "true", preserve = "true" *) logic f2a_source_original;
  (* keep = "true", preserve = "true" *) logic f2a_branch_observed;
  (* keep = "true", preserve = "true" *) logic f2a_input_A;
  (* keep = "true", preserve = "true" *) logic f2a_input_B1;
  (* keep = "true", preserve = "true" *) logic f2a_input_B2;
  (* keep = "true", preserve = "true" *) logic f2a_input_C2;
  (* keep = "true", preserve = "true" *) logic f2a_output_ZN;

  // Procedural mirrors force distinct, named VCD objects.
  always @* begin
    f2a_source_original = probe_source_original_i;
    f2a_branch_observed = probe_branch_observed_i;
    f2a_input_A = probe_input_A_i;
    f2a_input_B1 = probe_input_B1_i;
    f2a_input_B2 = probe_input_B2_i;
    f2a_input_C2 = probe_input_C2_i;
    f2a_output_ZN = probe_output_ZN_i;
  end

  initial begin
    if ($test$plusargs("local_probe")) begin
      $display("[F2A_PROBE] active fault=BF0001_SA0 module=cv32e40p_ff_one sink=g1227/C1");

      // tb_top calls $dumpfile/$dumpvars in the active region at time zero.
      // Move to the inactive region at the same simulation time before adding
      // these variables.  A positive delay would be too late for the VCD header.
      #0;

      $dumpvars(0, f2a_source_original);
      $dumpvars(0, f2a_branch_observed);
      $dumpvars(0, f2a_input_A);
      $dumpvars(0, f2a_input_B1);
      $dumpvars(0, f2a_input_B2);
      $dumpvars(0, f2a_input_C2);
      $dumpvars(0, f2a_output_ZN);

      $display(
        "[F2A_PROBE] requested VCD variables: f2a_source_original, f2a_branch_observed, f2a_input_A, f2a_input_B1, f2a_input_B2, f2a_input_C2, f2a_output_ZN"
      );
      $display("[F2A_PROBE] VCD registration completed at time %0t", $time);
    end
  end

endmodule

bind cv32e40p_ff_one f2a_local_probe f2a_local_probe_i (
    .probe_source_original_i(first_one_o[4]),
    .probe_branch_observed_i(first_one_o[4]),
    .probe_input_A_i(n_30),
    .probe_input_B1_i(n_68),
    .probe_input_B2_i(n_44),
    .probe_input_C2_i(n_58),
    .probe_output_ZN_i(first_one_o[2])
);
