from waitress import serve
from website import create_app

app = create_app()

if __name__ == "__main__":
    print("Starting Waitress server on http://127.0.0.1:5000 ...")
    # 'threads=8' allows up to 8 parallel requests simultaneously
    serve(app, host="127.0.0.1", port=5000, threads=8)