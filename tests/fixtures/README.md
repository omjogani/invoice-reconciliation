# Test fixtures

`ratecards/*.json` are hand-written rate cards used **only by unit and
integration tests**, to exercise the rate-card gates and the pricing engine
without spawning agents. The reconciliation flow never reads them. In a real
run, rate cards come from the contract-compilation agents and must match a
human-approved snapshot under `rate-cards/approved/`. See DESIGN.md.

Their `contract_sha256` is a placeholder; tests substitute the real hash of
the contract they are checked against.
