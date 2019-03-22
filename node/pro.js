var delay_print_first = function() {
    return "First";
}
var delay_print_second = function() {
    return Promise.resolve("Second");
}
var delay_print_third = function() {
    return Promise.resolve("Third");
}
var async_status = async function(ms) {
    var first = await delay_print_first();
    var send = await delay_print_second();
    var third = await delay_print_third();
    return first + " " + send + " " + third;
}

async_status().then((ret)=> {
    console.log(ret); // First Second Third
});
