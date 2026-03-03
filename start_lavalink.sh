#!/bin/bash
# start_lavalink.sh - Download and start Lavalink

# Inject Homebrew OpenJDK into PATH if it exists (Intel & Apple Silicon paths)
export PATH="/usr/local/opt/openjdk@17/bin:/opt/homebrew/opt/openjdk@17/bin:$PATH"

# Load environment variables for plugins (e.g., Spotify creds in application.yml placeholders).
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

LAVALINK_REPO="lavalink-devs/Lavalink"
LAVALINK_API_URL="https://api.github.com/repos/${LAVALINK_REPO}/releases/latest"
LAVALINK_JAR="Lavalink.jar"
LAVALINK_VERSION_FILE=".lavalink_version"

LATEST_TAG=$(
    curl -fsSL "$LAVALINK_API_URL" \
        | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        | head -n 1
)

if [ -z "$LATEST_TAG" ]; then
    if [ -f "$LAVALINK_JAR" ]; then
        echo "Could not resolve latest Lavalink tag from GitHub API. Using existing Lavalink.jar."
    else
        echo "Failed to resolve latest Lavalink tag from GitHub API."
        exit 1
    fi
fi

CURRENT_TAG=""
if [ -f "$LAVALINK_VERSION_FILE" ]; then
    CURRENT_TAG=$(tr -d '[:space:]' < "$LAVALINK_VERSION_FILE")
fi

if [ ! -f "$LAVALINK_JAR" ] || { [ -n "$LATEST_TAG" ] && [ "$CURRENT_TAG" != "$LATEST_TAG" ]; }; then
    if [ -z "$LATEST_TAG" ]; then
        echo "Cannot download Lavalink without a resolved tag."
        exit 1
    fi
    LAVALINK_URL="https://github.com/${LAVALINK_REPO}/releases/download/${LATEST_TAG}/Lavalink.jar"
    echo "Downloading Lavalink (${LATEST_TAG})..."
    curl -fL -o "$LAVALINK_JAR" "$LAVALINK_URL"
    if [ $? -ne 0 ]; then
        echo "Failed to download Lavalink. Please check your internet connection."
        exit 1
    fi
    printf "%s\n" "$LATEST_TAG" > "$LAVALINK_VERSION_FILE"
    echo "Lavalink downloaded successfully."
else
    if [ -n "$CURRENT_TAG" ]; then
        echo "Lavalink.jar is up to date (${CURRENT_TAG})."
    else
        echo "Lavalink.jar found. Skipping download."
    fi
fi

# Check for java installation
if ! command -v java &> /dev/null; then
    echo "Java is not installed! Please install Java 17+ to run Lavalink."
    exit 1
fi

JAVA_VERSION=$(java -version 2>&1 | awk -F '"' '/version/ {print $2}')
echo "Using Java version: $JAVA_VERSION"

echo "Starting Lavalink server..."
java -jar "$LAVALINK_JAR"
