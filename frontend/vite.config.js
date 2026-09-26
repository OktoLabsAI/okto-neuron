import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';
// Emits to ../frontend_dist (served by the Starlette server; devops wires the static mount).
// Dev proxy forwards /api to the local marginalia server (default :7777 per the contract).
export default defineConfig({
    plugins: [react()],
    resolve: {
        alias: { '@': path.resolve(__dirname, './src') },
    },
    build: {
        outDir: path.resolve(__dirname, '../frontend_dist'),
        emptyOutDir: true,
    },
    server: {
        port: 5180,
        proxy: {
            '/api': {
                target: process.env.VITE_API_TARGET || 'http://127.0.0.1:7777',
                changeOrigin: true,
            },
        },
    },
});
