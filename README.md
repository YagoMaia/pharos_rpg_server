# Pharos RPG Server

Backend server for a real-time RPG, built with FastAPI and WebSockets.

## Features

- Real-time communication via WebSockets.
- RPG game logic (combat, etc.).
- Uses Redis for data storage/caching.

## Technologies Used

- Python 3.12+
- FastAPI
- WebSockets
- Redis

## How to Run

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd pharos-rpg-server
   ```

2. **Install dependencies:**

   **Using `pip`:**
   ```bash
   pip install -r requirements.txt
   ```

   **Using `uv`:**
   ```bash
   uv sync
   ```

3. **Run the server:**
   ```bash
   uvicorn app.main:app --reload
   ```
