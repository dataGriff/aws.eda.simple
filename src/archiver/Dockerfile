# The archiver image: Bento's official image plus our config. Nothing is compiled and nothing
# runs at build time, so `docker build --platform linux/arm64` works on any host without
# emulation - the Fargate task is arm64, Floci runs whatever the host is.
#
# The base is busybox with a static Go binary (~40 MB), running as the unprivileged `bento`
# user; `wget` is there for the ECS health check. Pinned to the release mise.toml installs for
# `bento lint` and `bento test`, so the tests run the code that deploys.
FROM ghcr.io/warpstreamlabs/bento:1.21.2

COPY archiver.yaml /archiver.yaml

# Everything else - queue URL, bucket, region, credentials - comes from the environment.
CMD ["-c", "/archiver.yaml"]
