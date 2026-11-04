import asyncio
import websockets
import sys

# --- Настройки ---
SERVER_HOST = "0.0.0.0"  # Слушать на всех интерфейсах локальной сети
SERVER_PORT = 8765
CLIENT_URI = f"ws://{SERVER_HOST}:{SERVER_PORT}"

# =============================================================================
# СЕРВЕР КОД (Исправленный)
# =============================================================================

# *** ВНИМАНИЕ: Эта функция теперь корректно принимает ОБЕ необходимых переменные: websocket и path ***
async def handler(websocket):
    print(f"Новое соединение установлено от {websocket.remote_address}")
    try:
        # Чтение сообщений
        async for message in websocket:
            print(f"Получено: {message}")
            # Отправка эхо-сообщения
            await websocket.send(f"Эхо от сервера: {message}")
    except websockets.exceptions.ConnectionClosedOK:
        print("Соединение закрыто нормально.")
    except Exception as e:
        print(f"Произошла ошибка соединения: {e}")
    finally:
        print("Соединение завершено.")

async def main():
    # Здесь мы передаем handler, который теперь правильно принимает 2 аргумента
    async with websockets.serve(handler, SERVER_HOST, SERVER_PORT):
        print(f"WebSocket сервер запущен на ws://{SERVER_HOST}:{SERVER_PORT}")
        await asyncio.Future()  # Запускает сервер бесконечно

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nСервер остановлен пользователем.")
    except Exception as e:
        print(f"Критическая ошибка сервера: {e}")


# =============================================================================
# КЛИЕНТ КОД
# =============================================================================

async def client():
    print(f"Попытка подключения к {CLIENT_URI}...")
    try:
        async with websockets.connect(CLIENT_URI) as websocket:
            print("Успешно подключено к серверу!")
            while True:
                message = input("Введите сообщение для отправки (или 'exit' для выхода): ")
                if message.lower() == 'exit':
                    print("Отключение от сервера...")
                    break
                await websocket.send(message)

                response = await websocket.recv()
                print(f"Получено от сервера: {response}")

    except ConnectionRefusedError:
        print("Ошибка: Сервер не запущен или недоступен по указанному адресу.")
    except Exception as e:
        print(f"Произошла ошибка клиента: {e}")

if __name__ == "__main__":
    asyncio.run(client())