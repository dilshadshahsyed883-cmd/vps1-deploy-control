# EXPERIMENTAL: native Linux GPU-worker Concat engine, NOT Windows desktop EXE.
# Manual build only until actual Salad GPU hardware render has been tested.
FROM ubuntu:24.04 AS build
ARG DEBIAN_FRONTEND=noninteractive
ARG CONCAT_COMMIT=79116bd0f9a103c633d888603ad27890e929dbdc
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git build-essential pkg-config cmake clang libclang-dev libfontconfig1-dev libfreetype6-dev libxkbcommon-dev libgl1-mesa-dev libgtk-3-dev libasound2-dev libvulkan-dev python3 xz-utils bzip2 tar && rm -rf /var/lib/apt/lists/*
RUN git init /src && cd /src && git remote add origin https://github.com/jub0t/Concat.git && git fetch --depth=1 origin "$CONCAT_COMMIT" && git checkout --detach FETCH_HEAD && test "$(git rev-parse HEAD)" = "$CONCAT_COMMIT"
WORKDIR /src
# Dependency downloads below are checksum verified against upstream deps.lock.
RUN scripts/ci/fetch.sh ffmpeg-linux64 ffmpeg.tar.xz && mkdir -p ffmpeg-dev && tar -xf ffmpeg.tar.xz -C ffmpeg-dev --strip-components=1
RUN scripts/ci/fetch.sh onnxruntime-linux-x64 onnxruntime.tgz && tar -xzf onnxruntime.tgz
RUN set -eu; version="$(grep -A1 'name = "sherpa-onnx-sys"' src/Cargo.lock | sed -n 's/^version = "\([^"]*\)"/\1/p')"; scripts/ci/fetch.sh sherpa-onnx-linux-x64 sherpa.tar.bz2; mkdir -p sherpa; tar -xjf sherpa.tar.bz2 -C sherpa; test -d "sherpa/sherpa-onnx-v$version-linux-x64-static-lib/lib"
ENV FFMPEG_DIR=/src/ffmpeg-dev
ENV ORT_LIB_LOCATION=/src/onnxruntime-linux-x64-1.28.0/lib
ENV ORT_PREFER_DYNAMIC_LINK=1
ENV LD_LIBRARY_PATH=/src/ffmpeg-dev/lib:/src/onnxruntime-linux-x64-1.28.0/lib
RUN curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none
ENV PATH=/root/.cargo/bin:$PATH
RUN rustup toolchain install 1.93.0 --profile minimal && cd src && cargo +1.93.0 build --locked --release -p concat-cli -j 2
RUN test -x src/target/release/concat-cli && ldd src/target/release/concat-cli | tee /tmp/ldd.txt && ! grep -q "not found" /tmp/ldd.txt

FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libfontconfig1 libfreetype6 libxkbcommon0 libgl1 libgtk-3-0 libasound2 libvulkan1 libgomp1 libstdc++6 && rm -rf /var/lib/apt/lists/* && mkdir -p /worker/jobs /worker/outputs && chown -R 10001:10001 /worker
COPY --from=build /src/src/target/release/concat-cli /usr/local/bin/concat-cli
COPY --from=build /src/ffmpeg-dev/lib/ /opt/concat/ffmpeg/lib/
COPY --from=build /src/onnxruntime-linux-x64-1.28.0/lib/ /opt/concat/onnx/lib/
ENV LD_LIBRARY_PATH=/opt/concat/ffmpeg/lib:/opt/concat/onnx/lib
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility,video,graphics
WORKDIR /worker
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/concat-cli"]
CMD ["--help"]
