# Build from the apps/my-remote/server/ context.
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src
COPY MyRemote.Server.csproj ./
RUN dotnet restore MyRemote.Server.csproj
COPY . ./
RUN dotnet publish MyRemote.Server.csproj -c Release -o /output --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=build /output ./
ENV ASPNETCORE_HTTP_PORTS=8080
USER $APP_UID
EXPOSE 8080
ENTRYPOINT ["dotnet", "MyRemote.Server.dll"]
