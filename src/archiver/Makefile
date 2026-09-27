# Build recipe for `sam build` (Metadata.BuildMethod: makefile on ArchiverFunction).
#
# There is nothing to compile: the function is the Bento Lambda distribution - a single
# static Go binary named `bootstrap` - plus archiver.yaml. This downloads the pinned release
# once into a cache, verifies its checksum, and copies both files into the artifact dir.
#
# BENTO_ARCH must match where the function will run: arm64 for AWS (Architectures in
# template.yaml), the host's architecture for Floci, which runs functions natively.
# `task local:deploy` sets it; `sam build` on its own builds for AWS.

BENTO_VERSION ?= 1.21.2
BENTO_ARCH    ?= arm64
BENTO_CACHE   ?= $(HOME)/.cache/bento-lambda

# sha256 of bootstrap_<version>_linux_<arch>.zip, from the release's checksums file.
BENTO_SHA256_arm64 := 3d82bc17a0b05991c5e732617561b25354227eed396b61c03cfc93c1b9f2f8e4
BENTO_SHA256_amd64 := 97dc43ad300748247f35afee873774be4958cd448e5782a410f23e23549fbdf4

BENTO_ASSET := bootstrap_$(BENTO_VERSION)_linux_$(BENTO_ARCH).zip
BENTO_URL   := https://github.com/warpstreamlabs/bento/releases/download/v$(BENTO_VERSION)/$(BENTO_ASSET)
BENTO_ZIP   := $(BENTO_CACHE)/$(BENTO_ASSET)

.PHONY: build-ArchiverFunction
build-ArchiverFunction: $(BENTO_ZIP)
	unzip -o -q $(BENTO_ZIP) bootstrap -d $(ARTIFACTS_DIR)
	chmod 755 $(ARTIFACTS_DIR)/bootstrap
	cp archiver.yaml $(ARTIFACTS_DIR)/archiver.yaml

$(BENTO_ZIP):
	mkdir -p $(BENTO_CACHE)
	curl -fsSL -o $@.tmp $(BENTO_URL)
	echo "$(BENTO_SHA256_$(BENTO_ARCH))  $@.tmp" | shasum -a 256 -c -
	mv $@.tmp $@
