import type { Component } from 'vue'
import { packFrontends as runtimeFrontends } from '../packFrontends'


export async function loadGamePackRuntimeFrontend(
  frontendPlugin: string,
): Promise<Component> {
  if (!/^[a-z][a-z0-9_]{0,63}$/.test(frontendPlugin)) {
    throw new Error('游戏包前端标识无效')
  }
  const modulePath = `../plugins/${frontendPlugin}/runtime.ts`
  const loadModule = runtimeFrontends[modulePath]
  if (!loadModule) {
    throw new Error(`游戏包前端尚未安装：${frontendPlugin}`)
  }
  const loaded = await loadModule()
  if (!loaded.default) {
    throw new Error(`游戏包前端入口无效：${frontendPlugin}`)
  }
  return loaded.default
}
