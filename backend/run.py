import os

from app import create_app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.getenv("PLATFORM_HOST", "127.0.0.1"),
        port=int(os.getenv("PLATFORM_PORT", "5500")),
        debug=os.getenv("PLATFORM_DEBUG", "false").lower() == "true",
    )
