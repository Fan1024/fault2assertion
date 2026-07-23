`timescale 1ns/1ps

module nangate_cell_smoke_tb;

  logic ck = 1'b0;
  logic rn = 1'b1;
  logic d  = 1'b0;

  logic e  = 1'b0;
  logic se = 1'b1;

  wire q;
  wire gck;

  integer gck_posedges = 0;

  always #5 ck = ~ck;

  always @(posedge gck) begin
    gck_posedges = gck_posedges + 1;
  end

  DFFR_X1 u_ff (
    .RN (rn),
    .CK (ck),
    .D  (d),
    .Q  (q)
  );

  CLKGATETST_X1 u_clock_gate (
    .E   (e),
    .CK  (ck),
    .SE  (se),
    .GCK (gck)
  );

  initial begin
    $dumpfile("nangate_cell_smoke.vcd");
    $dumpvars(0, nangate_cell_smoke_tb);

    $display("[SMOKE] Starting Nangate cell test");

    // Generate an explicit 1 -> 0 reset edge.
    #2;
    rn = 1'b0;

    #1;
    if (q !== 1'b0) begin
      $fatal(
        1,
        "[SMOKE] DFFR_X1 reset failed: expected Q=0, got Q=%b",
        q
      );
    end

    // Release reset away from the active clock edge.
    #7;
    rn = 1'b1;
    d  = 1'b1;

    @(posedge ck);
    #1;

    if (q !== 1'b1) begin
      $fatal(
        1,
        "[SMOKE] DFFR_X1 capture failed: expected Q=1, got Q=%b",
        q
      );
    end

    d = 1'b0;

    @(posedge ck);
    #1;

    if (q !== 1'b0) begin
      $fatal(
        1,
        "[SMOKE] DFFR_X1 second capture failed: expected Q=0, got Q=%b",
        q
      );
    end

    repeat (3) @(posedge ck);
    #1;

    if (gck_posedges < 3) begin
      $fatal(
        1,
        "[SMOKE] CLKGATETST_X1 failed: SE=1 but GCK did not toggle"
      );
    end

    $display(
      "[SMOKE] PASS: DFFR_X1 and CLKGATETST_X1 are functional"
    );

    $finish;
  end

endmodule
