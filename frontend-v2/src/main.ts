import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'
import './styles/main.css'
import './styles/s1-theme.css'

createApp(App).use(createPinia()).use(router).mount('#app')

