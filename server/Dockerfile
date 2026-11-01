FROM python:3.13-alpine
RUN pip install --no-cache-dir paho-mqtt==2.1.0 pillow==11.3.0
COPY beatlight.py /app/beatlight.py
CMD ["python", "-u", "/app/beatlight.py"]
