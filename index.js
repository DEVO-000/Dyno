const express = require('express');
const axios = require('axios');
const cors = require('cors');

const app = express();

// Разрешаем вашему сайту обращаться к серверу
app.use(cors());

// Токен берется из настроек Render (Environment Variables)
const VK_TOKEN = process.env.VK_TOKEN;

app.get('/search', async (req, res) => {
    const query = req.query.q;

    if (!query) {
        return res.status(400).json({ error: 'Параметр q (запрос) обязателен' });
    }

    try {
        // Запрос к API ВКонтакте
        const response = await axios.get('https://api.vk.com/method/audio.search', {
            params: {
                q: query,
                access_token: VK_TOKEN,
                v: '5.131',
                count: 50 // Количество результатов
            },
            headers: {
                // Маскировка под Kate Mobile для получения полных ссылок (.mp3)
                'User-Agent': 'KateMobileAndroid/56 lite-armeabi-v7a (Android 4.4.2; SDK 19; armeabi-v7a; unknown; ru)',
                'Host': 'api.vk.com'
            }
        });

        // Проверка на ошибки от самого ВК
        if (response.data.error) {
            console.error('VK API Error:', response.data.error.error_msg);
            return res.status(500).json({ 
                error: 'Ошибка ВК', 
                message: response.data.error.error_msg 
            });
        }

        // Преобразуем данные из формата ВК в формат вашего плеера
        const tracks = response.data.response.items.map(track => ({
            id: track.id,
            artist: {
                name: track.artist
            },
            title: track.title,
            preview: track.url, // Ссылка на полный файл .mp3
            album: {
                cover_medium: track.album?.thumb?.photo_300 || 'https://via.placeholder.com/300',
                cover_xl: track.album?.thumb?.photo_600 || 'https://via.placeholder.com/600'
            },
            duration: track.duration
        }));

        // Отправляем готовый список песен на ваш сайт
        res.json(tracks);

    } catch (error) {
        console.error('Server Error:', error.message);
        res.status(500).json({ error: 'Ошибка прокси-сервера' });
    }
});

// Запуск на порту, который выдаст Render
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    console.log(`Сервер DEVO запущен на порту ${PORT}`);
});