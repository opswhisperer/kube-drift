# kor finds the orphaned resources (github.com/yonahd/kor); pinned by version + sha256 per arch.
FROM python:3.12-slim AS kor
ARG TARGETARCH
ARG KOR_VERSION=0.6.9
ARG KOR_SHA256_AMD64=6ca2c3f034a1cc16f12a42bb722028288c7a50a83131bd1662248b4c58f6d6a6
ARG KOR_SHA256_ARM64=f735868a033b01f259cff230e0fbc205467130d394ce4d72cdb163515ab67521
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) arch=x86_64; sum="$KOR_SHA256_AMD64" ;; \
      arm64) arch=arm64;  sum="$KOR_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], "/tmp/kor.tar.gz")' \
      "https://github.com/yonahd/kor/releases/download/v${KOR_VERSION}/kor_Linux_${arch}.tar.gz"; \
    echo "${sum}  /tmp/kor.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/kor.tar.gz -C /usr/local/bin kor; \
    chmod 755 /usr/local/bin/kor; \
    mkdir -p /usr/share/licenses/kor; \
    tar -xzf /tmp/kor.tar.gz -O LICENSE > /usr/share/licenses/kor/LICENSE

# Swagger UI for /docs (github.com/swagger-api/swagger-ui), from the npm swagger-ui-dist package.
FROM python:3.12-slim AS swagger
ARG SWAGGER_UI_VERSION=5.33.1
ARG SWAGGER_UI_SHA256=b468ff5f49451f194a739bc245c85b28c7d3d33057a8409a9b2369da37e215b9
RUN set -eu; \
    python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], "/tmp/swagger.tgz")' \
      "https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-${SWAGGER_UI_VERSION}.tgz"; \
    echo "${SWAGGER_UI_SHA256}  /tmp/swagger.tgz" | sha256sum -c -; \
    mkdir /swagger-ui; \
    tar -xzf /tmp/swagger.tgz -C /swagger-ui --strip-components=1 \
      package/swagger-ui-bundle.js package/swagger-ui.css package/LICENSE package/NOTICE \
      package/swagger-ui-bundle.js.LICENSE.txt

FROM python:3.12-slim

RUN groupadd -g 1001 kubedrift && useradd -u 1001 -g 1001 -M -s /usr/sbin/nologin kubedrift

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY --from=kor /usr/local/bin/kor /usr/local/bin/kor
COPY --from=kor /usr/share/licenses/kor/LICENSE /usr/share/licenses/kor/LICENSE
WORKDIR /srv
COPY --chmod=644 app/ ./app/
COPY --from=swagger --chmod=644 /swagger-ui/ ./app/static/swagger-ui/
RUN find /srv -type d -exec chmod 755 {} +

# No config is baked in: mount one at $CONFIG (see config/example.yaml), or run on defaults.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    CONFIG=/config/config.yaml \
    STATE_FILE=/data/drift.json \
    DATA_DIR=/data \
    HOME=/tmp

# The release tag (CI) or `git describe` (build.sh), shown in the dashboard header and the API.
ARG VERSION=dev
ENV KUBE_DRIFT_VERSION=$VERSION

USER 1001:1001
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"
CMD ["python3", "-m", "app.main"]
