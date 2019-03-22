// await 关键字后的函数
var Delay_Time = function(ms) {
    setTimeout(resolve, 1000)
}
// async 函数
var Delay_Print = function(ms) {
    Delay_Time(ms)
    return ("End");
}
// 执行async函数后
var resolve = Delay_Print(1000)
console.log(resolve);
