# Verified multi-architecture manifest digest (amd64/arm64 included).
ARG PYTHON_IMAGE=docker.io/library/python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
FROM ${PYTHON_IMAGE} AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY meco_news ./meco_news
RUN python -m pip install --no-cache-dir --no-deps setuptools==75.8.0 wheel==0.45.1 \
    && python -m pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir /build/wheels .

FROM ${PYTHON_IMAGE} AS runtime

WORKDIR /app
COPY --from=builder /build/wheels/meco_news-*.whl /tmp/
RUN python -m pip install --no-cache-dir --no-index --no-deps /tmp/meco_news-*.whl \
    && rm -f /tmp/meco_news-*.whl \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin meco \
    && mkdir -p /app/data /app/config \
    && chown -R meco:meco /app/data \
    && chmod 0555 /app /usr/local/lib/python3.13/site-packages/meco_news \
    && chmod -R a=rX /usr/local/lib/python3.13/site-packages/meco_news
COPY config ./config
RUN chown -R root:root /app/config && chmod -R a=rX /app/config

USER meco
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "meco_news"]
CMD ["--daemon"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 CMD ["python", "-m", "meco_news", "--healthcheck", "--json"]
