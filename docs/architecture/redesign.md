# New Requirements

This application is meant to be a standalone API that handles everything related to email transactions. A user will forward emails to a unique email address associated with their account. The application will then process these emails and extract transactions.

This API is a component in a larger budgeting application and should assume that there is a client budgeting app that will be using this transaction data to drive its data.

The budgeting app now has the capability to allow users to define their own categories for transactions and we eventually want the users to be able to confirm the cvategory of transactions coming from the email API and eventually get to place where we can use heuristics to make better predictions going forward. Ideally I would not want to use AI for parsing the emails unless you think it would be crucial for accuracy. I think that we should aim for very high accuracy since these are financial transactions and errors will lead to users getting annoyed and not using the system.
