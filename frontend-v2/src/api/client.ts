import axios from 'axios'

const apiClient = axios.create({
  baseURL: '/api',
  timeout: 15_000,
})

apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem('ai-rpg-token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

export interface ApiEnvelope<T> {
  code: number
  message: string
  data: T
}

export default apiClient

