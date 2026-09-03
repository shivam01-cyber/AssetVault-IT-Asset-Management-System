"""ASGI entrypoint. Wraps the Flask (WSGI) app so the platform's uvicorn
supervisor process can serve it on 0.0.0.0:8001."""
from asgiref.wsgi import WsgiToAsgi

from app import flask_app

app = WsgiToAsgi(flask_app)
