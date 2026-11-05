# Triage Demo (repro app)

A one-screen Compose app that triggers 15 of the golden-set crash families on purpose, so the eval can use real captures instead of synthetic logs.

Not built in CI: open this folder in Android Studio, let it sync (accept any version bumps it suggests), and run on a device or emulator.

## Capturing a crash for the held-out set

1. Run a scenario (each row says how).
2. Save the crash:
   ```bash
   adb logcat -d -b crash > ../golden/crashes/t01_state_loss_device.txt
   ```
   For ANRs (`c25`), take `adb bugreport` and copy the relevant trace from `FS/data/anr/`.
3. Add a label line to `../golden/labels.jsonl` with `"split": "test"`.
4. Do this **after** freezing `rules.yaml` and the prompt, or the test split stops being held out.

Release builds are minified (`isMinifyEnabled = true`). Crash a release build to get an obfuscated trace, and keep `app/build/outputs/mapping/release/mapping.txt` next to it in `golden/mappings/`.

Not covered here: native crashes (needs an NDK module) and Room migrations (needs Room + two schema versions).
