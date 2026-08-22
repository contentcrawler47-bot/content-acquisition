name: Probe — UML and diagram structure

# STAGE 1 of the PlantUML work. Answers four questions before any generator is
# written:
#
#   Q1  Can a Class be resolved to its Attributes and Operations?
#   Q2  Is Message ORDER recoverable? (the one genuine unknown — a sequence
#       diagram in the wrong order is worse than no diagram at all)
#   Q3  What do diagram objects contain, and how large do they get?
#   Q4  Which relation verbs connect what, so they can be mapped to PlantUML?
#
# References no secrets. Prints structure — field names, types, counts, values
# truncated to 80 chars — rather than documentation prose. Delete once the
# generator exists.

on:
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: probe-uml
  cancel-in-progress: true

jobs:
  probe:
    name: Can we generate PlantUML from this model?
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v5
        with:
          persist-credentials: false

      - name: Probe UML and diagram structure
        run: python3 tools/probe_uml.py
