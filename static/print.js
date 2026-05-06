(function () {
    document.addEventListener("click", function (event) {
        const trigger = event.target.closest("[data-print]");
        if (!trigger) {
            return;
        }
        event.preventDefault();
        window.print();
    });
})();
