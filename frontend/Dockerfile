FROM node:22-alpine AS frontend-vendor
WORKDIR /vendor
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY ./src /usr/share/nginx/html
COPY --from=frontend-vendor /vendor/node_modules/paper/dist/paper-full.min.js /usr/share/nginx/html/vendor/paper-full.min.js

EXPOSE 80
