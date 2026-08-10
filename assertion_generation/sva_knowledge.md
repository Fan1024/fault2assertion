Use only the supplied signal aliases: `site_i` and `recv_N_i`.

Useful SVA constructs:
- `|->`  overlapped implication
- `|=>`  next-cycle implication
- `##N`  exact cycle delay
- `$past`, `$stable`, `$rose`, `$fell`

Generate only one property-expression body.

Do not emit:
- clock/reset syntax
- `assert property`
- modules
- semicolons
- raw hierarchy names
- absolute simulation cycle numbers
