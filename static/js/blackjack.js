function connectWS() {
    const ws = new WebSocket(`wss://arcade.1010819.xyz/game/blackjack/ws`);
    
    ws.onclose = () => {
        console.log("斷線了，1秒後嘗試重連...");
        setTimeout(connectWS, 1000); 
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        // 處理資料...
    };
}
