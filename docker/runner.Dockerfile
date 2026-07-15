FROM gcc:14.2.0-bookworm@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c AS common

COPY docker/runner/runner.cpp /src/runner.cpp
RUN g++ -std=c++17 -O2 -Wall -Wextra -Werror /src/runner.cpp -o /usr/local/bin/contest-runner

COPY testlib/testlib.h /opt/testlib/testlib.h
COPY jngen/jngen.h /opt/jngen/jngen.h

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin runner \
    && mkdir -p /workspace \
    && chown runner:runner /workspace

USER runner
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/contest-runner"]

FROM common AS compiler
USER root
RUN g++ -std=c++17 -O2 -pipe -Wall -Wextra \
      -x c++-header /opt/jngen/jngen.h -o /opt/jngen/jngen.h.gch \
    || rm -f /opt/jngen/jngen.h.gch
USER runner

FROM common AS runtime-libraries
USER root
RUN mkdir -p /runtime-root \
    && ldd /usr/local/bin/contest-runner \
      | awk '$1 ~ /^(libstdc\+\+|libgcc_s)/ {print $3}' \
      | sort -u \
      | xargs -r -I '{}' cp --parents '{}' /runtime-root

FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818 AS executor

COPY --from=common /usr/local/bin/contest-runner /usr/local/bin/contest-runner
COPY --from=runtime-libraries /runtime-root/ /
ENV LD_LIBRARY_PATH=/usr/local/lib64

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin runner \
    && mkdir -p /workspace \
    && chown runner:runner /workspace

USER runner
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/contest-runner"]
