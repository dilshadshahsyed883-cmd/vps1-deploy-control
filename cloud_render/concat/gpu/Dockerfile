# Candidate Linux renderer, pinned to the exact v0.2.8 Windows editor engine.
# No production promotion: build and CPU startup smoke do not prove Salad GPU output.
FROM ubuntu:24.04 AS build
ARG DEBIAN_FRONTEND=noninteractive
ARG CONCAT_COMMIT=79116bd0f9a103c633d888603ad27890e929dbdc
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git build-essential pkg-config cmake clang libclang-dev libfontconfig1-dev libfreetype6-dev libxkbcommon-dev libxkbcommon-x11-0 libgl1-mesa-dev libgtk-3-dev libasound2-dev libvulkan-dev python3 xz-utils bzip2 tar && rm -rf /var/lib/apt/lists/*
RUN git init /src && cd /src && git remote add origin https://github.com/jub0t/Concat.git && git fetch --depth=1 origin "$CONCAT_COMMIT" && git checkout --detach FETCH_HEAD && test "$(git rev-parse HEAD)" = "$CONCAT_COMMIT"
WORKDIR /src
RUN scripts/ci/fetch.sh ffmpeg-linux64 ffmpeg.tar.xz && mkdir -p ffmpeg-dev && tar -xf ffmpeg.tar.xz -C ffmpeg-dev --strip-components=1
RUN scripts/ci/fetch.sh onnxruntime-linux-x64 onnxruntime.tgz && tar -xzf onnxruntime.tgz && test -f onnxruntime-linux-x64-1.28.0/lib/libonnxruntime.so.1.28.0 && ln -sf libonnxruntime.so.1.28.0 onnxruntime-linux-x64-1.28.0/lib/libonnxruntime.so
RUN set -eu; version="$(grep -A1 'name = "sherpa-onnx-sys"' src/Cargo.lock | sed -n 's/^version = "\([^"]*\)"/\1/p')"; scripts/ci/fetch.sh sherpa-onnx-linux-x64-static-lib sherpa.tar.bz2; mkdir -p sherpa; tar -xjf sherpa.tar.bz2 -C sherpa; test -d "sherpa/sherpa-onnx-v$version-linux-x64-static-lib/lib"
ENV FFMPEG_DIR=/src/ffmpeg-dev
ENV ORT_LIB_LOCATION=/src/onnxruntime-linux-x64-1.28.0/lib
ENV ORT_PREFER_DYNAMIC_LINK=1
ENV SHERPA_ONNX_LIB_DIR=/src/sherpa/sherpa-onnx-v1.13.7-linux-x64-static-lib/lib
ENV LD_LIBRARY_PATH=/src/ffmpeg-dev/lib:/src/onnxruntime-linux-x64-1.28.0/lib
RUN curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none
ENV PATH=/root/.cargo/bin:$PATH
RUN rustup toolchain install 1.93.0 --profile minimal && cd src && cargo +1.93.0 build --locked --release -p concat-cli -j 2
RUN test -x src/target/release/concat-cli && ldd src/target/release/concat-cli | tee /tmp/ldd.txt && ! grep -q "not found" /tmp/ldd.txt

FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl python3 libfontconfig1 libfreetype6 libxkbcommon0 libxkbcommon-x11-0 libgl1 libgtk-3-0t64 libasound2t64 libvulkan1 libgomp1 libstdc++6 && rm -rf /var/lib/apt/lists/* && mkdir -p /worker/jobs /worker/outputs /worker/state && chown -R 10001:10001 /worker
COPY --from=build /src/src/target/release/concat-cli /usr/local/bin/concat-cli
COPY --from=build /src/ffmpeg-dev/lib/ /opt/concat/ffmpeg/lib/
COPY --from=build /src/onnxruntime-linux-x64-1.28.0/lib/ /opt/concat/onnx/lib/
COPY --from=build /src/LICENSE /usr/share/doc/concat/LICENSE
COPY cloud_render/concat/project_bundle.py /opt/app/cloud_render/concat/project_bundle.py
COPY cloud_render/concat/gpu/worker.py /opt/app/cloud_render/concat/gpu/worker.py
ENV LD_LIBRARY_PATH=/opt/concat/ffmpeg/lib:/opt/concat/onnx/lib
ENV PYTHONPATH=/opt/app
ENV PYTHONUNBUFFERED=1
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility,video,graphics
ENV PORT=8080
WORKDIR /worker
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python3", "-m", "cloud_render.concat.gpu.worker"]
