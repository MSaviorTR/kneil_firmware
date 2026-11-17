# kneil_tinyml tests

| File | What it is |
|---|---|
| `kneil_test_vectors.h` | GENERATED (do not edit): 27 raw 2 s windows (50 Hz, g, device frame), their expected 20 features and the expected class of the forest and of the single tree |
| `host_test.c` | runs the self-test on the PC and, optionally, a stream file through the classifier |
| `run_host_test.py` | builds and runs `host_test.c` for both models with gcc (Windows: MinGW-w64, e.g. WinLibs) |
| `run_host_test.sh` | the same for Linux/macOS |

```
python tests/run_host_test.py       -> HOST TEST: PASS (both models)
```
The same vectors are embedded in the firmware with `CONFIG_KNEIL_TINYML_SELFTEST=y` (`kneil_selftest()`), and the
KNEIL live firmware also plays them through the classifier for its boot-time timing test.
See [../docs/kneil_tinyml.md](../docs/kneil_tinyml.md).
