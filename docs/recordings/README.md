# Recorded runs

The documentation quotes real output. These files are the complete, unedited
standard output of the runs it quotes, so that every figure and every answer
can be checked against its source. A test (`tests/test_docs.py`) fails if the
README's transcript or the quoted figures stop matching them.

| File | What it is |
| --- | --- |
| [quickstart-smollm2-360m.txt](quickstart-smollm2-360m.txt) | The run shown in the [README](../../README.md): train on the notes in [examples/data](../../examples/data) with the default settings and 30 epochs, inspect the vault, then ask five questions with and without the adapter. All four answers about the notes are right. |
| [weak-settings-smollm2-360m.txt](weak-settings-smollm2-360m.txt) | The same notes, model and key with adapters on the attention projections only and 20 epochs. The loss stops at 0.935, and three of the four answers about the notes are fluent, confident and wrong. |

Each file starts with the versions and the hardware it was recorded on. They are
single runs. Training is seeded, so the losses should repeat on the same
software versions; timings will not, and other versions of the libraries can
change both the numbers and the answers.

To make a new recording, run the commands of a file (the lines that start with
`$`) in a directory that holds `notes.txt` and `qa.jsonl`, and capture standard
output.
