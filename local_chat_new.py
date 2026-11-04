import asyncio
import websockets
import sys
import time # Импортируем для задержки

# --- Настройки ---
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8765
CLIENT_URI = f"ws://{SERVER_HOST}:{SERVER_PORT}"

# --- Функция для печати текста с анимацией ---
async def type_writer(message: str, delay: float = 0.03):
    """Печатает текст посимвольно в консоли."""
    for char in message:
        print(char, end="", flush=True)
        await asyncio.sleep(delay)
    print() # Переход на новую строку после завершения печати

# =============================================================================
# СЕРВЕР КОД
# =============================================================================

async def handler(websocket):
    print(f"Новое соединение установлено от {websocket.remote_address}")
    try:
        async for message in websocket:
            print(f"Получено: {message}")
            # Отправляем обратно то же сообщение (эхо)
            await websocket.send(f"Эхо от сервера: {message}")
    except websockets.exceptions.ConnectionClosedOK:
        print("Соединение закрыто нормально.")
    except Exception as e:
        print(f"Произошла ошибка соединения: {e}")
    finally:
        print("Соединение завершено.")

async def main():
    async with websockets.serve(handler, SERVER_HOST, SERVER_PORT):
        print(f"WebSocket сервер запущен на ws://{SERVER_HOST}:{SERVER_PORT}")
        await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nСервер остановлен пользователем.")
    except Exception as e:
        print(f"Критическая ошибка сервера: {e}")


# =============================================================================
# КЛИЕНТ КОД (С анимацией)
# =============================================================================

async def client():
    print(f"Попытка подключения к {CLIENT_URI}...")
    try:
        async with websockets.connect(CLIENT_URI) as websocket:
            print("Успешно подключено к серверу!")
            while True:
                # 1. Сообщение, которое пользователь вводит
                user_message = input("Введите сообщение для отправки (или 'exit' для выхода): ")
                
                if user_message.lower() == 'exit':
                    print("Отключение от сервера...")
                    break
                
                # 2. Анимация печати сообщения от клиента
                await type_writer(f"👤 {user_message}", delay=0.02) 
                
                # 3. Отправка сообщения на сервер
                await websocket.send(user_message)

                # 4. Получение ответа от сервера (используем обычный recv, так как ответ сервера придет сразу)
                response = await websocket.recv()
                
                # 5. Анимация печати ответа сервера
                await type_writer(f"🤖 {response}", delay=0.03)

    except ConnectionRefusedError:
        print("Ошибка: Сервер не запущен или недоступен по указанному адресу.")
    except Exception as e:
        print(f"Произошла ошибка клиента: {e}")

if __name__ == "__main__":
    asyncio.run(client())