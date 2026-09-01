const { generate } = require('youtube-po-token-generator');

generate()
    .then(token => {
        console.log(JSON.stringify(token));
    })
    .catch(error => {
        console.error(JSON.stringify({ error: error.message }));
    });