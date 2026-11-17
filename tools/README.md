# kneil_tinyml tools (Python, run on the PC)

| Script | What it does |
|---|---|
| `train_kneil_tinyml.py` | trains the forest and the single tree (scikit-learn), exports them to C with emlearn (`include/kneil_model_*.h`); options `--fs <Hz>`, `--lowpass <Hz>`, `--extra <real CSV folder>`, `--compare` |
| `gen_test_vectors.py` | writes `tests/kneil_test_vectors.h` from `tools/test_vectors.npz` (raw windows, features, expected classes) |

Requirements: `pip install emlearn scikit-learn scipy pandas`. The training data (synthetic KNEIL dataset and real
recordings) is not in this repository. Model V3 itself was exported by a separate pipeline (`stage3.py`), which
writes the same files. After a new export: copy the files, run `python tests/run_host_test.py`, rebuild the
firmware and check the on-board self-test. See [../docs/kneil_tinyml.md](../docs/kneil_tinyml.md).
