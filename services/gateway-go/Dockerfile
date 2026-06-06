# Pure-Go build (modernc sqlite => no cgo needed)
FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY main.go ./
# Build a static binary (modernc sqlite => CGO not required)
RUN CGO_ENABLED=0 go build -o /gateway .

FROM gcr.io/distroless/static-debian12
COPY --from=build /gateway /gateway
EXPOSE 8080
ENTRYPOINT ["/gateway"]
