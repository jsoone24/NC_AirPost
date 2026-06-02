# Building PX4 v1.17.0 SITL + Gazebo Harmonic on macOS (Apple Silicon)

Reproducible steps + the source patches needed to build PX4 **v1.17.0** SITL with the
**gz_bridge** (Gazebo Harmonic) on macOS, using Homebrew's gz/protobuf/abseil.

## Environment
- macOS (Apple Silicon, arm64), Homebrew
- Gazebo **Harmonic** `gz-sim8` (`brew install gz-harmonic` / already present)
- Python **3.10** via pyenv (PX4 tooling breaks on 3.12+; system was 3.14)
- `ninja`, `cmake`, `opencv`, `gstreamer` via brew

## Steps
```bash
cd PX4-Autopilot
git checkout v1.17.0 && git submodule update --init --recursive

brew install ninja opencv gstreamer   # gz-harmonic assumed already installed
gst-inspect-1.0 >/dev/null            # pre-build GStreamer registry cache (one-time)

# Python 3.10 venv for the build tooling
$(pyenv root)/versions/3.10.18/bin/python -m venv .venv
.venv/bin/python -m pip install -U pip wheel setuptools
.venv/bin/python -m pip install -r Tools/setup/requirements.txt

# build (venv + brew on PATH)
export PATH="$PWD/.venv/bin:/opt/homebrew/bin:$PATH"
make px4_sitl_default
```

## Patches applied (macOS portability — diverge from the stock v1.17.0 tag)

Root cause: PX4 v1.17's gz build assumes Linux (GCC + apt-pinned deps + `.so`). On macOS it's
Apple Clang + Homebrew (newest gz/protobuf 35/abseil 2026) + `.dylib`, so (a) Clang-only or
newer-API warnings fire and PX4's `-Werror` turns them fatal, and (b) library names/paths are
hardcoded for Linux. All of it is fixable — every gz plugin builds and loads on macOS.

1. **`src/modules/simulation/gz_msgs/CMakeLists.txt`** — generated protobuf needs C++17 and
   abseil headers trip `-Werror`:
   ```cmake
   target_compile_features(px4_gz_msgs PUBLIC cxx_std_17)
   target_compile_options(px4_gz_msgs PRIVATE -Wno-error)
   ```

2. **`src/modules/simulation/gz_bridge/CMakeLists.txt`** — newer protobuf deprecates
   `Resize`, plus float→double promotions; add to the module `COMPILE_FLAGS`:
   ```
   -Wno-error=double-promotion -Wno-error=deprecated-declarations -Wno-error=float-conversion
   ```

3. **`src/modules/simulation/gz_plugins/moving_platform_controller/MovingPlatformController.cpp:243`**
   — explicit cast to silence `-Werror=double-promotion`:
   ```cpp
   _force += feedback_force * gz::math::Vector3d(static_cast<double>(scaling), static_cast<double>(scaling), 1.);
   ```

4. **`optical_flow` plugin** — the external `OpticalFlow` lib builds fine on macOS but as
   `libOpticalFlow.dylib`; PX4 hardcoded `.so`. In `optical_flow/optical_flow.cmake` replace
   both `libOpticalFlow.so` with `libOpticalFlow${CMAKE_SHARED_LIBRARY_SUFFIX}` (cross-platform).
   Also `-Wno-error` on the target (Clang-only `-Wunused-private-field`). Needs `brew opencv`.

5. **`gstreamer` (GstCamera) plugin** — needs `brew gstreamer` (gstreamer-1.0 + app-1.0). In
   `gstreamer/CMakeLists.txt`: add `-Wno-error`, and `target_link_directories(... ${GSTREAMER_LIBRARY_DIRS}
   ${GSTREAMER_APP_LIBRARY_DIRS})` — Homebrew libs aren't in the default linker path (Linux finds
   them in /usr/lib without this).

6. **`gz_bridge/server.config`** — the two custom plugins were referenced as `libXxx.so`
   (Linux-only). Changed to the base name (`OpticalFlowSystem`, `GstCameraSystem`) so gz adds
   the platform suffix (`.dylib`/`.so`) automatically on both OSes.

7. **`gz_bridge/gz_env.sh.in`** — `export GST_REGISTRY_FORK=no`. GstCamera calls `gst_init()`
   at load; the forked `gst-plugin-scanner` is slow on macOS (and warns), pushing gz world-init
   past PX4's startup timeout. In-process scan + the pre-warmed registry keeps startup fast.

> The `-Wno-error` items mask warnings in third-party-facing generated/plugin code; the rest
> are genuine cross-platform fixes (`.so`→suffix, link dirs, base names). With these, a clean
> `make px4_sitl_default` builds **all** gz plugins and the sim loads them with zero errors.
