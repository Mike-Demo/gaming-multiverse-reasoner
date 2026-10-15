FROM python:3.12-slim
WORKDIR /app
COPY app.py co2.min.js co2-footer.js ./
EXPOSE 8080
CMD ["python3", "app.py"]
