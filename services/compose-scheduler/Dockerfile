FROM --platform=$BUILDPLATFORM golang:1.26.11-alpine3.24 AS build
ARG TARGETOS
ARG TARGETARCH
RUN apk add --no-cache ca-certificates
WORKDIR /src
COPY go.mod main.go ./
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags='-s -w -buildid=' -o /out/scheduler .

FROM scratch
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/scheduler /scheduler
USER 65532:65532
ENV GITHUB_TOKEN_FILE=/run/secrets/github_dispatch_token
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s CMD ["/scheduler", "--healthcheck"]
ENTRYPOINT ["/scheduler"]
