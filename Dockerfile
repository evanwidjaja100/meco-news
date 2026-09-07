# The default is an immutable multi-architecture manifest digest.  A release
# may override this only with another reviewed digest, never with a tag.
ARG PYTHON_IMAGE=docker.io/library/python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f
FROM ${PYTHON_IMAGE} AS builder

WORKDIR /build
COPY pyproject.toml README.md requirements-build.lock ./
COPY meco_news ./meco_news
RUN python -m pip install --no-cache-dir --require-hashes --no-deps -r requirements-build.lock \
    && python -m pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir /build/wheels .

FROM ${PYTHON_IMAGE} AS runtime

WORKDIR /app
COPY --from=builder /build/wheels/meco_news-*.whl /tmp/
RUN python -m pip install --no-cache-dir --no-index --no-deps /tmp/meco_news-*.whl \
    && rm -f /tmp/meco_news-*.whl \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin meco \
    && mkdir -p /app/data /app/config \
    && chown -R meco:meco /app/data \
    && chmod 0555 /app /usr/local/lib/python3.14/site-packages/meco_news \
    && chmod -R a=rX /usr/local/lib/python3.14/site-packages/meco_news
COPY config ./config
RUN chown -R root:root /app/config && chmod -R a=rX /app/config

USER meco
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "meco_news"]
CMD ["--daemon"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 CMD ["python", "-m", "meco_news", "--healthcheck", "--json"]
