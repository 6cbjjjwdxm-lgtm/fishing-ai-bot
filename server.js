require('dotenv').config();
console.log('dotenv загружен');

const express = require('express');
const TelegramBot = require('node-telegram-bot-api');

const token = process.env.TELEGRAM_TOKEN;
console.log('Токен:', token ? 'OK' : 'ПУСТОЙ!');

if (!token) {
  console.log('ОШИБКА: TELEGRAM_TOKEN отсутствует');
  process.exit(1);
}

const bot = new TelegramBot(token, {webHook: true});

const app = express();
app.use(express.json());

app.post('/webhook', (req
