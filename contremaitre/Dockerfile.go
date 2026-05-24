# Contremaitre Go-capable runtime image.
#
# Extends contremaitre-agent:latest with a Go toolchain. Use when the
# target repo has a go.sum and the check-cmd is `go test ./...` or
# similar.
#
# Build:
#   contremaitre image build --variant go
#   contremaitre image build --variant go --no-cache
#
# Use: pass --docker-image contremaitre-agent-go:latest to `run` /
# `tui run`. Module deps are cached in a contremaitre-deps-go-sum-*
# volume populated by `go mod download`; GOPATH points at it.

FROM contremaitre-agent:latest

ARG GO_VERSION=1.23.4
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-$(dpkg --print-architecture).tar.gz" \
        -o /tmp/go.tgz \
    && tar -C /usr/local -xzf /tmp/go.tgz \
    && rm /tmp/go.tgz

ENV PATH="/usr/local/go/bin:${PATH}"

# Smoke-test the install.
RUN go version
