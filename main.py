# backend/main.py
import asyncio, random, string, json
from typing import List, Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel, Field, Relationship, Session, create_engine, select
from sqlmodel import SQLModel, Field, Relationship


DB_URL = "sqlite:///bingo.db"
engine = create_engine(DB_URL, echo=False)

# ---------- modelos SQLite ----------



class Card(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    numbers: str
    user_username: str = Field(foreign_key="user.username")  # clave foránea basada en username
    active: bool = True 
    user: Optional["User"] = Relationship(back_populates="cards")


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)

    cards: List[Card] = Relationship(back_populates="user")

class GameHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    winner_username: str

SQLModel.metadata.create_all(engine)

# ---------- utilidades ----------
def generate_card():
    cols = {
        'B': random.sample(range(1, 16), 5),
        'I': random.sample(range(16, 31), 5),
        'N': random.sample(range(31, 46), 5),
        'G': random.sample(range(46, 61), 5),
        'O': random.sample(range(61, 76), 5),
    }
    cols['N'][2] = 0
    grid = []
    for i in range(5):
        row = []
        for col in "BINGO":
            num = cols[col][i]
            cell = f"{col}{num}" if num else "FREE"
            row.append(cell)
        grid.append(row)
    return grid

def all_balls():
    balls = [f"{l}{n}" for l, start in zip("BINGO", [1,16,31,46,61]) for n in range(start, start+15)]
    random.shuffle(balls)
    return balls

# ---------- estado de la partida ----------
remaining: List[str] = all_balls()
called: List[str] = []
clients: List[WebSocket] = []
winner: Optional[str] = None

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=True)

@app.post("/join")
def join(username: str):
    with Session(engine) as s:
        stmt = select(User).where(User.username == username)
        user = s.exec(stmt).first()
        if not user:
            user = User(username=username)
            s.add(user); s.commit(); s.refresh(user)

        # Desactivar tarjetas anteriores
        for c in user.cards:
            s.delete(c)  # o c.active = False si querés mantenerlas
        s.commit()

        # Crear nueva tarjeta
        new_card = Card(
            numbers=json.dumps(generate_card()),
            user_username=user.username
        )
        s.add(new_card); s.commit(); s.refresh(new_card)
        return {"card": json.loads(new_card.numbers)}

@app.get("/called")
def get_called():
    return called

@app.get("/last_call")
def get_last_call():
    return called[-1] if called else None

@app.get("/history")
def get_history():
    with Session(engine) as s:
        return s.exec(select(GameHistory)).all()

@app.post("/reset")
def reset_game():
    global remaining, called, winner
    remaining = all_balls()
    called = []
    winner = None
    with Session(engine) as s:
        cards = s.exec(select(Card).where(Card.active == True)).all()
        for c in cards:
            c.active = False
            s.add(c)
        s.commit()
    return {"msg": "Partida reiniciada"}

@app.websocket("/ws/{username}")
async def game_ws(ws: WebSocket, username: str):
    await ws.accept()
    clients.append(ws)
    for c in called:
        await ws.send_json({"type":"call", "value":c})

    try:
        global winner
        while True:
            msg = await ws.receive_json()
            match msg.get("type"):
                case "next":
                    if winner:
                        await ws.send_json({"type":"info", "value":"Ya hay un ganador."})
                        continue
                    if not remaining:
                        await ws.send_json({"type":"info", "value":"Ya no quedan bolitas."})
                        continue
                    call = remaining.pop()
                    called.append(call)
                    for c in clients:
                        await c.send_json({"type":"call", "value":call})

                case "bingo":
                    if winner:
                        await ws.send_json({"type":"error", "value":"Ya hay un ganador."})
                        continue
                    marked = msg.get("marked", [])
                    grid = msg.get("grid", [])
                    if not all(m in called or m == "FREE" for m in marked):
                        await ws.send_json({"type": "error", "value": "Casillas no válidas 🙈"})
                        return

                    bool_grid = [[cell in marked or cell == "FREE" for cell in row] for row in grid]
                    def is_line_full(line): return all(line)
                    bingo_valido = any(
                        is_line_full(row) for row in bool_grid
                    ) or any(
                        is_line_full(col) for col in zip(*bool_grid)
                    ) or all(bool_grid[i][i] for i in range(5)) or all(bool_grid[i][4-i] for i in range(5))

                    if bingo_valido:
                        winner = username
                        with Session(engine) as s:
                            s.add(GameHistory(winner_username=winner))
                            s.commit()
                        for c in clients:
                            await c.send_json({"type": "winner", "value": winner})
                    else:
                        await ws.send_json({"type": "error", "value": "¡No es una línea válida!"})

    except WebSocketDisconnect:
        clients.remove(ws)

@app.on_event("startup")
def create_admin():
    with Session(engine) as s:
        if not s.exec(select(User).where(User.username == "admin")).first():
            user = User(username="admin")
            s.add(user)
            s.commit()
