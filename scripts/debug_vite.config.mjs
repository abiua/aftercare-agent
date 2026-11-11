// Preview the normal frontend build against the isolated business debugger.
export default {
  preview: {
    host: '127.0.0.1',
    port: 15192,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:18092',
        changeOrigin: true,
      },
    },
  },
};
