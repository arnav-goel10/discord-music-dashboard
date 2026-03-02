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

LAVALINK_URL="https://github.com/lavalink-devs/Lavalink/releases/latest/download/Lavalink.jar"
LAVALINK_JAR="Lavalink.jar"

if [ ! -f "$LAVALINK_JAR" ]; then
    echo "Downloading Lavalink..."
    curl -L -o "$LAVALINK_JAR" "$LAVALINK_URL"
    if [ $? -ne 0 ]; then
        echo "Failed to download Lavalink. Please check your internet connection."
        exit 1
    fi
    echo "Lavalink downloaded successfully."
else
    echo "Lavalink.jar found. Skipping download."
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
